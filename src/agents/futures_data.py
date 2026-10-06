"""期貨／選擇權行情：期交所 MIS 即時報價（主要）＋期交所 OpenAPI 每日行情與保證金（備援）。

合約代碼：
- 期貨 MIS 代號 TXFJ6-F：J＝10 月（A～L 對應 1～12 月）、6＝2026 年。系統內部合約寫成 TMF202610。
- 選擇權 TXO46000V6-O：買權月份 A～L、賣權月份 M～X（V＝10 月賣權），履約價 46000。系統內部寫成 TXO202610P46000。
- 月合約結算日：每月第三個星期三。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

import requests

from src.config import now_tw, rules
from src.data import providers

log = logging.getLogger(__name__)
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
MIS_CID = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
CALL_MONTHS = "ABCDEFGHIJKL"
PUT_MONTHS = "MNOPQRSTUVWX"
TAIFEX = "期交所"
# 期交所 API 失敗時的備用保證金（2026-10-02 公告值，僅在抓不到時使用）
FALLBACK_MARGIN = {"TX": {"initial": 701000, "maintenance": 538000}, "MTX": {"initial": 175250, "maintenance": 134500},
                   "TMF": {"initial": 35050, "maintenance": 26900}}
_C: dict[str, tuple[float, object]] = {}


def _cache(key: str, ttl: float, fn):
    hit = _C.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    if val:
        _C[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- 合約月份
def settlement_date(ym: str) -> dt.date:
    y, m = int(ym[:4]), int(ym[4:6])
    d = dt.date(y, m, 1)
    first_wed = d + dt.timedelta(days=(2 - d.weekday()) % 7)
    return first_wed + dt.timedelta(days=14)


def front_month(today: dt.date | None = None, min_days: int = 0) -> str:
    """近月合約月份；距結算不到 min_days 天時改用次月（轉倉）。"""
    today = today or now_tw().date()
    ym = f"{today.year}{today.month:02d}"
    if today > settlement_date(ym) or (settlement_date(ym) - today).days < min_days:
        nxt = (dt.date(today.year, today.month, 28) + dt.timedelta(days=7)).replace(day=1)
        ym = f"{nxt.year}{nxt.month:02d}"
    return ym


def days_to_settlement(ym: str, today: dt.date | None = None) -> int:
    return (settlement_date(ym) - (today or now_tw().date())).days


def _ym_from_code(letter: str, ydigit: str, months: str) -> str:
    m = months.index(letter) + 1
    year = 2020 + int(ydigit)
    if year < now_tw().year - 1:  # 年份只有個位數，跨十年時往後推
        year += 10
    return f"{year}{m:02d}"


def _fmt_time(d: str, t: str) -> str:
    """20261002 + 134459 → 2026-10-02 13:44:59"""
    d, t = str(d or ""), str(t or "").zfill(6) if t else ""
    out = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
    return f"{out} {t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 else out


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 即時報價
def _mis(payload: dict) -> list[dict]:
    r = requests.post(MIS_URL, json={"MarketType": "0", "KindID": "1", "RowSize": "全部", "PageNo": "", "SortColumn": "",
                                     "AscDesc": "A", "ExpireMonth": "", **payload}, headers=UA, timeout=20)
    return r.json().get("RtData", {}).get("QuoteList", [])


def futures_quotes(root: str) -> dict:
    """{ym: {price, ref, chg_pct, time}}＋spot（加權現貨）。MIS 失敗時改用每日行情（盤後結算價）。"""
    def fetch():
        out: dict = {"months": {}, "spot": None, "source": f"{TAIFEX} MIS 即時"}
        try:
            for x in _mis({"SymbolType": "F", "CID": MIS_CID[root]}):
                sid = x["SymbolID"]
                if sid.endswith("-S"):
                    out["spot"] = _f(x["CLastPrice"])
                    continue
                m = re.match(r"^[A-Z]+([A-L])(\d)-F$", sid)
                if not m or "W" in x.get("DispCName", ""):  # 週合約略過
                    continue
                ym = _ym_from_code(m.group(1), m.group(2), CALL_MONTHS)
                ref, last = _f(x["CRefPrice"]), _f(x["CLastPrice"])
                price = last or ref
                if price:
                    out["months"][ym] = {"price": price, "ref": ref, "settlement": _f(x.get("SettlementPrice")),
                                         "date": x.get("CDate", ""),
                                         "chg_pct": round((price / ref - 1) * 100, 2) if ref else None,
                                         "time": _fmt_time(x.get("CDate", ""), x.get("CTime", ""))}
            providers._mark(f"{TAIFEX} MIS", True)
        except Exception as e:  # noqa: BLE001
            providers._mark(f"{TAIFEX} MIS", False, str(e))
            log.warning("期交所 MIS %s 失敗：%s", root, e)
        if not out["months"]:
            out.update(_daily_futures(root))
        return out if out["months"] else None

    return _cache(f"fq_{root}", 8, fetch) or {"months": {}, "spot": None, "source": "無資料"}


def _daily_futures(root: str) -> dict:
    try:
        rows = requests.get("https://openapi.taifex.com.tw/v1/DailyMarketReportFut", timeout=40).json()
        months = {}
        for x in rows:
            if x["Contract"] != root or "/" in x["ContractMonth(Week)"] or "W" in x["ContractMonth(Week)"]:
                continue
            if x.get("TradingSession") == "盤後":
                continue
            p = _f(x.get("SettlementPrice")) or _f(x.get("Last"))
            if p:
                months[x["ContractMonth(Week)"]] = {"price": p, "ref": None, "chg_pct": None, "time": x["Date"],
                                                     "settlement": _f(x.get("SettlementPrice")), "date": x["Date"]}
        providers._mark(f"{TAIFEX} OpenAPI", True)
        return {"months": months, "source": f"{TAIFEX} OpenAPI 每日行情"}
    except Exception as e:  # noqa: BLE001
        providers._mark(f"{TAIFEX} OpenAPI", False, str(e))
        return {"months": {}}


def quote(root: str, ym: str | None = None) -> dict:
    q = futures_quotes(root)
    ym = ym or front_month()
    m = q["months"].get(ym) or {}
    return {"root": root, "contract": f"{root}{ym}", "ym": ym, **m, "spot": q.get("spot"),
            "basis": round(m["price"] - q["spot"], 2) if m.get("price") and q.get("spot") else None,
            "source": q.get("source"), "days_to_settlement": days_to_settlement(ym)}


def put_chain(ym: str) -> list[dict]:
    """台指選擇權賣權報價（月選）。"""
    def fetch():
        out = []
        try:
            for x in _mis({"SymbolType": "O", "CID": "TXO", "ExpireMonth": ym}):
                m = re.match(r"^TXO(\d+)([M-X])(\d)-O$", x["SymbolID"])
                if not m or _ym_from_code(m.group(2), m.group(3), PUT_MONTHS) != ym:
                    continue
                bid, ask, last, ref = (_f(x.get(k)) for k in ("CBestBidPrice", "CBestAskPrice", "CLastPrice", "CRefPrice"))
                strike = float(m.group(1))
                out.append({"strike": strike, "contract": f"TXO{ym}P{int(strike)}", "bid": bid, "ask": ask,
                            "last": last, "ref": ref, "price": ask or last or ref})
            providers._mark(f"{TAIFEX} MIS", True)
        except Exception as e:  # noqa: BLE001
            providers._mark(f"{TAIFEX} MIS", False, str(e))
        return sorted(out, key=lambda r: r["strike"])

    return _cache(f"puts_{ym}", 30, fetch) or []


def margins() -> dict:
    """{TX: {initial, maintenance}}（期交所公告，6 小時快取；失敗用備用值並註明）。"""
    def fetch():
        try:
            rows = requests.get("https://openapi.taifex.com.tw/v1/IndexFuturesAndOptionsMargining", timeout=30).json()
            by_name = {r["Contract"]: r for r in rows}
            out = {}
            for root, spec in rules()["futures"]["contracts"].items():
                r = by_name.get(spec.get("margin_name", ""))
                if r:
                    out[root] = {"initial": float(r["InitialMargin"]), "maintenance": float(r["MaintenanceMargin"]),
                                 "date": r.get("Date"), "source": f"{TAIFEX} OpenAPI"}
            providers._mark(f"{TAIFEX} OpenAPI", True)
            return out
        except Exception as e:  # noqa: BLE001
            providers._mark(f"{TAIFEX} OpenAPI", False, str(e))
            return {}

    got = _cache("margins", 6 * 3600, fetch)
    return got or {k: {**v, "source": "備用值（期交所 2026-10-02 公告）"} for k, v in FALLBACK_MARGIN.items()}


def day_session_open(now: dt.datetime | None = None) -> bool:
    from src.config import is_trading_day

    now = now or now_tw()
    return is_trading_day(now.date()) and dt.time(8, 45) <= now.time() <= dt.time(13, 45)


def marks(pf) -> dict[str, float]:
    """持有期貨／選擇權的評價價格（給 Portfolio.nav 使用）。
    日盤交易中用即時成交價；其餘時間用日盤結算價（與競賽 App 相同：10/06 結算 50,082、未實現 82,800）。"""
    out = {}
    live = day_session_open()
    for c, d in pf.derivs.items():
        if d.kind == "FUT":
            q = quote(d.root, c[len(d.root):])
            p = q.get("price") if live else (q.get("settlement") or q.get("price"))
        else:
            ym = c[3:9]
            p = next((r["last"] or r["ref"] for r in put_chain(ym) if r["contract"] == c), None)
        if p:
            out[c] = p
    return out


# ---------------------------------------------------------------- 歷史日 K（近月連續）
def _hist_chunk(root: str, start: dt.date, end: dt.date) -> list[list[str]]:
    """期交所「期貨每日交易行情」下載（一次最多約 30 天）。過去的區間永久快取。"""
    import csv
    import io
    import pickle

    from src.config import CACHE_DIR

    path = CACHE_DIR / f"fut_{root}_{start:%Y%m%d}_{end:%Y%m%d}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    try:
        r = requests.post("https://www.taifex.com.tw/cht/3/futDataDown", timeout=60,
                          headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.taifex.com.tw/cht/3/dlFutDataDown"},
                          data={"down_type": "1", "commodity_id": root, "commodity_id2": "",
                                "queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": end.strftime("%Y/%m/%d")})
        rows = list(csv.reader(io.StringIO(r.content.decode("cp950", errors="replace"))))[1:]
        providers._mark(f"{TAIFEX} 歷史行情", bool(rows))
    except Exception as e:  # noqa: BLE001
        providers._mark(f"{TAIFEX} 歷史行情", False, str(e))
        return []
    if rows and end < now_tw().date() - dt.timedelta(days=1):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(rows))
    return rows


def history(root: str, days: int = 260) -> list[dict]:
    """近月連續日 K：每天取一般交易時段、成交量最大的月合約（不含價差與週合約）。"""
    def fetch():
        today = now_tw().date()
        start = today - dt.timedelta(days=int(days * 1.5))
        rows, end = [], today
        while end > start:
            s = max(start, end - dt.timedelta(days=29))
            # 對齊到固定區間，過去的區間才能快取命中
            rows += _hist_chunk(root, s, end)
            end = s - dt.timedelta(days=1)
        best: dict[str, list[str]] = {}
        for r in rows:
            if len(r) < 18 or r[17].strip() != "一般" or "/" in r[2] or "W" in r[2]:
                continue
            try:
                vol = float(r[9])
            except ValueError:
                continue
            if r[0] not in best or vol > float(best[r[0]][9]):
                best[r[0]] = r
        out = []
        for d in sorted(best):
            r = best[d]
            o, h, lo, c = (_f(x) for x in r[3:7])
            if not all((o, h, lo, c)):
                continue
            out.append({"time": d.replace("/", "-"), "open": o, "high": h, "low": lo, "close": c, "volume": float(r[9]),
                        "contract": f"{root}{r[2].strip()}", "settlement": _f(r[10]), "oi": _f(r[11])})
        return out[-days:]

    return _cache(f"hist_{root}_{days}", 1800, fetch) or []
