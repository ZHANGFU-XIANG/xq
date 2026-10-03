"""UniverseAgent：全市場（上市＋上櫃＋ETF）股票池與歷史 K 線。

1. FinMind TaiwanStockInfo → 代號、名稱、產業、市場
2. 證交所／櫃買中心當日行情 → 依成交值初篩（排除權證、ETN、槓桿反向 ETF）
3. yfinance 批次下載歷史日 K（每日增量更新，快取於 data/cache/panel.pkl）
4. 依 20 日均成交值與股價做最終流動性篩選；排除處置股
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import pickle
import re
import time

import pandas as pd
import requests
import yfinance as yf

from src.config import CACHE_DIR, settings

log = logging.getLogger(__name__)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
PANEL_PKL = CACHE_DIR / f"panel_{settings()['universe']['history_years']}y.pkl"  # 年數變更時自動重建
INFO_PKL = CACHE_DIR / "stock_info.pkl"
CODE_RE = re.compile(r"^(\d{4}|00\d{2,4}[A-Z]?)$")

_info: dict[str, dict] | None = None


def _finmind_headers() -> dict:
    tok = os.getenv("FINMIND_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def stock_info() -> dict[str, dict]:
    """{code: {name, industry, market(TWSE/TPEx), is_etf}}，每日快取。"""
    global _info
    if _info is not None:
        return _info
    if INFO_PKL.exists() and time.time() - INFO_PKL.stat().st_mtime < 86400:
        _info = pickle.loads(INFO_PKL.read_bytes())
        return _info
    info: dict[str, dict] = {}
    try:
        r = requests.get(FINMIND_URL, params={"dataset": "TaiwanStockInfo"}, headers=_finmind_headers(), timeout=60)
        for row in r.json()["data"]:
            if row["type"] not in ("twse", "tpex"):
                continue
            code = row["stock_id"]
            ind = row["industry_category"]
            is_etf = code.startswith("00") or "ETF" in ind or "指數股票型" in ind
            cur = info.get(code)
            # 同一代號可能有多個產業列，保留非 ETF/非「其他」的那一個
            if cur and cur["industry"] not in ("其他", "電子工業") and ind in ("其他", "電子工業"):
                continue
            info[code] = {"code": code, "name": row["stock_name"], "industry": "ETF" if is_etf else ind,
                          "raw_industry": ind, "market": "TWSE" if row["type"] == "twse" else "TPEx", "is_etf": is_etf}
    except Exception as e:  # noqa: BLE001
        log.error("取得股票清單失敗：%s", e)
        if INFO_PKL.exists():
            _info = pickle.loads(INFO_PKL.read_bytes())
            return _info
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INFO_PKL.write_bytes(pickle.dumps(info))
    _info = info
    return info


def _today_values() -> dict[str, float]:
    """最近交易日各股成交值（元）。"""
    vals: dict[str, float] = {}
    try:
        for x in requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=30).json():
            if x.get("TradeValue"):
                vals[x["Code"]] = float(x["TradeValue"])
    except Exception as e:  # noqa: BLE001
        log.warning("證交所行情失敗：%s", e)
    for attempt in range(3):
        try:
            for x in requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=60).json():
                v = (x.get("TransactionAmount") or "").replace(",", "")
                if v.replace(".", "").isdigit():
                    vals[x["SecuritiesCompanyCode"]] = float(v)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("櫃買行情失敗(%d)：%s", attempt, e)
            time.sleep(5)
    return vals


def punished_codes() -> set[str]:
    """處置股（分盤撮合、需預收款，流動性差，排除）。"""
    out: set[str] = set()
    try:
        for x in requests.get("https://openapi.twse.com.tw/v1/announcement/punish", timeout=30).json():
            out.add(x.get("Code", ""))
    except Exception as e:  # noqa: BLE001
        log.warning("處置股名單失敗：%s", e)
    return out


def candidate_codes() -> list[str]:
    cfg = settings()["universe"]
    info = stock_info()
    vals = _today_values()
    out = []
    for code, m in info.items():
        if not CODE_RE.match(code) or m["raw_industry"] in cfg["exclude_industries"]:
            continue
        if m["is_etf"] and (not cfg["include_etf"] or any(k in m["name"] for k in cfg["exclude_name_keywords"])):
            continue
        if vals.get(code, 0) >= cfg["prefilter_min_value"]:
            out.append(code)
    return sorted(out)


def _yf_symbol(code: str) -> str:
    m = stock_info().get(code, {})
    return code + (".TWO" if m.get("market") == "TPEx" else ".TW")


def _download(codes: list[str], period: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(codes), 150):
        chunk = codes[i:i + 150]
        syms = [_yf_symbol(c) for c in chunk]
        for attempt in range(2):
            try:
                d = yf.download(syms, period=period, group_by="ticker", threads=True, progress=False, auto_adjust=False)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("批次下載失敗(%s)：%s", attempt, e)
                time.sleep(5)
        else:
            continue
        for c, s in zip(chunk, syms):
            try:
                df = d[s][["Open", "High", "Low", "Close", "Volume"]].dropna()
            except KeyError:
                continue
            df = df[df.Volume > 0]
            if not df.empty:
                df.index = pd.to_datetime(df.index.date)
                out[c] = df.astype(float)
    return out


def load_panel(codes: list[str] | None = None, refresh: bool = True) -> dict[str, pd.DataFrame]:
    """全市場日 K。已有快取者只下載近 1 個月增量。"""
    cfg = settings()["universe"]
    panel: dict[str, pd.DataFrame] = pickle.loads(PANEL_PKL.read_bytes()) if PANEL_PKL.exists() else {}
    if not refresh and panel:
        return panel
    codes = codes if codes is not None else candidate_codes()
    stale = not PANEL_PKL.exists() or time.time() - PANEL_PKL.stat().st_mtime > 3 * 3600
    new = [c for c in codes if c not in panel]
    if new:
        log.info("下載 %d 檔完整歷史", len(new))
        panel.update(_download(new, f"{cfg['history_years']}y"))
    if stale:
        old = [c for c in codes if c in panel and c not in new]
        log.info("增量更新 %d 檔", len(old))
        for c, df in _download(old, "1mo").items():
            merged = pd.concat([panel[c], df])
            panel[c] = merged[~merged.index.duplicated(keep="last")].sort_index()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PANEL_PKL.write_bytes(pickle.dumps(panel))
    return panel


def liquid_universe(panel: dict[str, pd.DataFrame], asof: dt.date) -> list[str]:
    cfg = settings()["universe"]
    banned = punished_codes()
    out = []
    for c, df in panel.items():
        d = df[df.index < pd.Timestamp(asof)]
        if len(d) < 260 or c in banned:
            continue
        val20 = float((d.Close * d.Volume).tail(20).mean())
        if val20 >= cfg["min_avg_value20"] and d.Close.iloc[-1] >= cfg["min_price"]:
            out.append(c)
    return sorted(out)


def meta(code: str) -> dict:
    """名稱、產業、市場與隔夜美股驅動因子。"""
    s = settings()
    m = dict(stock_info().get(code, {"code": code, "name": code, "industry": "其他", "market": "TWSE",
                                       "is_etf": code.startswith("00")}))
    m["sector"] = m.get("industry", "其他")
    drivers = s["industry_drivers"].get(m["sector"], s["industry_drivers"]["default"])
    for w in s.get("watchlist", []):
        if w["code"] == code:
            m["name"] = w.get("name", m["name"])
            drivers = w.get("drivers", drivers)
    m["drivers"] = drivers
    return m
