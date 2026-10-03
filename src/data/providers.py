"""資料來源備援：每種資料都有「FinMind（多組 token 輪流）→ 官方免費來源」的來源鏈。

- FinMind：環境變數 FINMIND_TOKENS（逗號分隔，相容 FINMIND_TOKEN），依序輪流；某組回報額度用完就冷卻 1 小時，
  全部冷卻時最後再試一次匿名請求，仍不行就改用官方來源。
- 官方來源（不需金鑰、沒有次數限制）：證交所 OpenAPI／rwd、櫃買中心 OpenAPI／www。全市場一次抓取並快取，
  每檔查詢只是查表，不會因為股票多而大量呼叫。
- 每次取得資料都記錄實際來源（source_of），供 AI 助理標註出處與「資料來源狀態」頁面使用。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import pickle
import threading
import time

import pandas as pd
import requests

from src.config import CACHE_DIR, is_trading_day

log = logging.getLogger(__name__)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TOKEN_COOLDOWN = 3600
UA = {"User-Agent": "Mozilla/5.0"}

FINMIND = "FinMind"
TWSE = "證交所 OpenAPI"
TPEX = "櫃買中心 OpenAPI"

_COOL: dict[str, float] = {}       # token 代號 → 冷卻到何時
_NEXT = {"i": 0}
_STATUS: dict[str, dict] = {}      # 來源 → {ok_at, fail_at, msg}
_SOURCE: dict[tuple[str, str], str] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 狀態
def _mark(source: str, ok: bool, msg: str = "") -> None:
    s = _STATUS.setdefault(source, {"ok_at": None, "fail_at": None, "msg": ""})
    now = dt.datetime.now().strftime("%m/%d %H:%M")
    if ok:
        s["ok_at"] = now
    else:
        s["fail_at"], s["msg"] = now, msg[:120]


def note_source(kind: str, code: str, source: str) -> None:
    _SOURCE[(kind, code)] = source


def source_of(kind: str, code: str) -> str | None:
    return _SOURCE.get((kind, code))


def status() -> dict:
    toks = _tokens()
    now = time.time()
    return {
        "finmind_tokens": len([t for t in toks if t]),
        "finmind_available": finmind_available(),
        "finmind_cooling": [{"token": _label(t), "until": dt.datetime.fromtimestamp(_COOL[_label(t)]).strftime("%H:%M")}
                            for t in toks if _COOL.get(_label(t), 0) > now],
        "sources": [{"source": k, **v} for k, v in sorted(_STATUS.items())],
    }


# ---------------------------------------------------------------- FinMind 輪流
def _tokens() -> list[str | None]:
    raw = os.getenv("FINMIND_TOKENS") or os.getenv("FINMIND_TOKEN") or ""
    toks = [t.strip() for t in raw.split(",") if t.strip()]
    return toks + [None]  # 最後是匿名請求（額度較少，但仍可用）


def _label(tok: str | None) -> str:
    return f"token{_tokens().index(tok) + 1}" if tok else "匿名"


def finmind_available() -> bool:
    now = time.time()
    return any(_COOL.get(_label(t), 0) <= now for t in _tokens())


def finmind_get(dataset: str, data_id: str | None = None, start: str | None = None) -> pd.DataFrame | None:
    """輪流使用 token。回傳 DataFrame（可能為空＝真的沒資料）；所有 token 都不可用或失敗時回傳 None。"""
    toks = _tokens()
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start:
        params["start_date"] = start
    with _LOCK:
        order = toks[_NEXT["i"] % len(toks):] + toks[:_NEXT["i"] % len(toks)]
        _NEXT["i"] += 1
    for tok in order:
        lab = _label(tok)
        if _COOL.get(lab, 0) > time.time():
            continue
        try:
            r = requests.get(FINMIND_URL, params=params, timeout=30,
                             headers={"Authorization": f"Bearer {tok}"} if tok else {})
            j = r.json()
        except Exception as e:  # noqa: BLE001
            _mark(FINMIND, False, str(e))
            continue
        if j.get("status") == 200:
            _mark(FINMIND, True)
            return pd.DataFrame(j.get("data", []))
        msg = str(j.get("msg", ""))
        _mark(FINMIND, False, f"{lab}：{msg}")
        if "upper limit" in msg or j.get("status") in (402, 429):
            _COOL[lab] = time.time() + TOKEN_COOLDOWN
            log.warning("FinMind %s 額度用完，冷卻 %d 分鐘，改用下一組", lab, TOKEN_COOLDOWN // 60)
            continue
        return None  # 其他錯誤（參數、資料集不存在）換 token 也沒用
    return None


# ---------------------------------------------------------------- 官方來源工具
def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _get_json(url: str, **kw):
    r = requests.get(url, timeout=40, headers=UA, **kw)
    r.raise_for_status()
    return r.json()


def _disk(key: str, fn, max_age: float | None):
    """全市場資料快取在磁碟；max_age=None 代表永久（過去日期的資料不會再變）。空結果不快取。"""
    path = CACHE_DIR / f"off_{key}.pkl"
    if path.exists() and (max_age is None or time.time() - path.stat().st_mtime < max_age):
        return pickle.loads(path.read_bytes())
    val = fn()
    if val:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(val))
    return val


_MEM: dict[str, tuple[float, object]] = {}


def _mem(key: str, ttl: float, fn):
    hit = _MEM.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    if val:
        _MEM[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- 三大法人（逐日全市場）
def _inst_twse(d: dt.date) -> dict:
    j = _get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                  params={"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
    out = {}
    for r in j.get("data") or []:
        f = (_num(r[4]) or 0) + (_num(r[7]) or 0)
        out[r[0].strip()] = (f, _num(r[10]) or 0.0, _num(r[11]) or 0.0, _num(r[18]) or 0.0)
    return out


def _inst_tpex(d: dt.date) -> dict:
    j = _get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                  params={"type": "Daily", "sect": "EW", "date": d.strftime("%Y/%m/%d"), "response": "json"})
    out = {}
    tables = j.get("tables") or []
    if not tables or str(j.get("date", "")) != d.strftime("%Y%m%d"):
        return out
    for r in tables[0].get("data") or []:
        # 欄位：10 外資及陸資合計、13 投信、22 自營商合計、23 三大法人合計（股）
        out[r[0].strip()] = (_num(r[10]) or 0.0, _num(r[13]) or 0.0, _num(r[22]) or 0.0, _num(r[23]) or 0.0)
    return out


def inst_day(d: dt.date) -> dict:
    """某一交易日全市場三大法人買賣超（股）：{code: (外資, 投信, 自營商, 合計)}。"""
    def fetch():
        out = {}
        for name, fn in ((TWSE, _inst_twse), (TPEX, _inst_tpex)):
            try:
                part = fn(d)
                out.update(part)
                if part:
                    _mark(name, True)
            except Exception as e:  # noqa: BLE001
                _mark(name, False, str(e))
                log.warning("%s 法人 %s 失敗：%s", name, d, e)
            time.sleep(0.4)  # 證交所有頻率限制
        return out

    final = d < dt.date.today() or dt.datetime.now().hour >= 17  # 當天 17:00 後才定案
    return _mem(f"inst_{d:%Y%m%d}", 86400 if final else 1800,
                lambda: _disk(f"inst_{d:%Y%m%d}", fetch, None if final else 1800)) or {}


def institutional(code: str, days: int = 12) -> pd.DataFrame:
    """近 N 個交易日的法人買賣超（欄位與 FinMind 版相同：foreign, trust, dealer, total）。"""
    rows, d, tried = [], dt.date.today(), 0
    while len(rows) < days and tried < days * 2 + 6:
        if is_trading_day(d):
            day = inst_day(d)
            if code in day:
                f, t, de, tot = day[code]
                rows.append({"date": pd.Timestamp(d), "foreign": f, "trust": t, "dealer": de, "total": tot})
        d -= dt.timedelta(days=1)
        tried += 1
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date").sort_index()


# ---------------------------------------------------------------- 月營收、本益比、融資券（全市場最新一期）
def _roc_ym(s: str) -> str:
    s = str(s).strip()
    return f"{int(s[:-2]) + 1911}/{s[-2:]}"


def _roc_date(s: str) -> str:
    s = str(s).strip()
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"


def revenue_all() -> dict:
    def fetch():
        out = {}
        for name, url in ((TWSE, "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"),
                          (TPEX, "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O")):
            try:
                for r in _get_json(url):
                    rev = _num(r.get("營業收入-當月營收"))
                    yoy, mom = _num(r.get("營業收入-去年同月增減(%)")), _num(r.get("營業收入-上月比較增減(%)"))
                    if rev is None or yoy is None:
                        continue
                    out[r["公司代號"].strip()] = {
                        "month": _roc_ym(r["資料年月"]), "revenue_100m": round(rev * 1000 / 1e8, 2),  # 單位：千元
                        "yoy": round(yoy / 100, 4), "mom": round(mom / 100, 4) if mom is not None else None,
                        "yoy_3m": None}
                _mark(name, True)
            except Exception as e:  # noqa: BLE001
                _mark(name, False, str(e))
        return out

    return _mem("rev", 6 * 3600, lambda: _disk("revenue", fetch, 6 * 3600))


def valuation_all() -> dict:
    def fetch():
        out = {}
        try:
            for r in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
                out[r["Code"].strip()] = {"date": _roc_date(r["Date"]), "per": _num(r.get("PEratio")),
                                          "pbr": _num(r.get("PBratio")), "dividend_yield": _num(r.get("DividendYield"))}
            _mark(TWSE, True)
        except Exception as e:  # noqa: BLE001
            _mark(TWSE, False, str(e))
        try:
            for r in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"):
                out[r["SecuritiesCompanyCode"].strip()] = {
                    "date": _roc_date(r["Date"]), "per": _num(r.get("PriceEarningRatio")),
                    "pbr": _num(r.get("PriceBookRatio")), "dividend_yield": _num(r.get("YieldRatio"))}
            _mark(TPEX, True)
        except Exception as e:  # noqa: BLE001
            _mark(TPEX, False, str(e))
        return out

    return _mem("val", 3 * 3600, lambda: _disk("valuation", fetch, 3 * 3600))


def margin_all() -> dict:
    """融資融券餘額（張）。官方只提供當日與前一日，因此給 1 日變化；5 日變化為 None。"""
    def fetch():
        out = {}
        try:
            for r in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"):
                mb, mp = _num(r.get("融資今日餘額")), _num(r.get("融資前日餘額"))
                sb, sp = _num(r.get("融券今日餘額")) or 0, _num(r.get("融券前日餘額")) or 0
                if mb is None:
                    continue
                out[r["股票代號"].strip()] = {"date": None, "margin_balance": int(mb), "margin_chg1": int(mb - (mp or mb)),
                                             "margin_chg5": None, "short_balance": int(sb), "short_chg1": int(sb - sp),
                                             "short_chg5": None}
            _mark(TWSE, True)
        except Exception as e:  # noqa: BLE001
            _mark(TWSE, False, str(e))
        try:
            for r in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"):
                mb, mp = _num(r.get("MarginPurchaseBalance")), _num(r.get("MarginPurchaseBalancePreviousDay"))
                sb, sp = _num(r.get("ShortSaleBalance")) or 0, _num(r.get("ShortSaleBalancePreviousDay")) or 0
                if mb is None:
                    continue
                out[r["SecuritiesCompanyCode"].strip()] = {
                    "date": _roc_date(r["Date"]), "margin_balance": int(mb), "margin_chg1": int(mb - (mp or mb)),
                    "margin_chg5": None, "short_balance": int(sb), "short_chg1": int(sb - sp), "short_chg5": None}
            _mark(TPEX, True)
        except Exception as e:  # noqa: BLE001
            _mark(TPEX, False, str(e))
        return out

    return _mem("margin", 3 * 3600, lambda: _disk("margin", fetch, 3 * 3600))
