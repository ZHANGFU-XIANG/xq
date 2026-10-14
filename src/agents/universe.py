"""UniverseAgent：全市場（上市＋上櫃＋ETF）股票池與歷史 K 線。

1. FinMind TaiwanStockInfo → 代號、名稱、產業、市場
2. 證交所／櫃買中心當日行情 → 依成交值初篩（排除權證、ETN、槓桿反向 ETF）
3. yfinance 批次下載歷史日 K（每日增量更新，快取於 data/cache/panel.pkl）
4. 依 20 日均成交值與股價做最終流動性篩選；排除處置股
"""
from __future__ import annotations

import datetime as dt
import logging
import pickle
import re
import time

import pandas as pd
import requests

from src.config import CACHE_DIR, last_closed_session, settings

log = logging.getLogger(__name__)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
PANEL_PKL = CACHE_DIR / f"panel_{settings()['universe']['history_years']}y.pkl"  # 年數變更時自動重建
INFO_PKL = CACHE_DIR / "stock_info.pkl"
CODE_RE = re.compile(r"^(\d{4}|00\d{2,4}[A-Z]?)$")

_info: dict[str, dict] | None = None




def _isin_info() -> dict[str, dict]:
    """備援：證交所 ISIN 公開清單（上市 strMode=2、上櫃 strMode=4），不需金鑰、沒有次數限制。
    CFI 代碼 ES 開頭＝普通股，CE 開頭＝ETF；權證、債券等其他類別略過。"""
    import html

    info: dict[str, dict] = {}
    for mode, market in ((2, "TWSE"), (4, "TPEx")):
        r = requests.get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", timeout=60)
        text = r.content.decode("cp950", errors="replace")
        for row in re.findall(r"<tr>(.*?)</tr>", text, re.S):
            cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) < 6 or "　" not in cells[0]:
                continue
            code, name = cells[0].split("　", 1)
            cfi, ind = cells[5], cells[4]
            if cfi.startswith("ES") and ind:
                is_etf = False
            elif cfi.startswith("CE"):
                is_etf, ind = True, "ETF"
            else:
                continue
            info[code.strip()] = {"code": code.strip(), "name": name.strip(), "industry": ind, "raw_industry": ind,
                                  "market": market, "is_etf": is_etf}
    return info


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
        from src.data import providers

        fm = providers.finmind_get("TaiwanStockInfo")
        for row in ([] if fm is None else fm.to_dict("records")):
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
        log.warning("FinMind 股票清單失敗（%s），改用證交所 ISIN 清單", str(e)[:120])
    if len(info) < 500:  # FinMind 額度用完時會回傳錯誤訊息而不是清單
        try:
            info = _isin_info()
            log.info("證交所 ISIN 清單：%d 檔", len(info))
        except Exception as e:  # noqa: BLE001
            log.error("取得股票清單失敗：%s", e)
    if len(info) < 500:
        if INFO_PKL.exists():  # 兩個來源都失敗：沿用舊快取（不論多舊）
            _info = pickle.loads(INFO_PKL.read_bytes())
            return _info
        return info  # 不寫入快取，下次再試
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
    """經 yahoo_gateway 批次下載（限流＋被限流時只重抓缺的代號）。"""
    from src.data import yahoo_gateway as yg

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(codes), 150):
        chunk = codes[i:i + 150]
        sym_of = {_yf_symbol(c): c for c in chunk}
        for s, df in yg.download(list(sym_of), period=period).items():
            try:
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            except KeyError:
                continue
            df = df[df.Volume > 0]
            if not df.empty:
                df.index = pd.to_datetime(df.index.date)
                out[sym_of[s]] = df.astype(float)
    return out


def load_panel(codes: list[str] | None = None, refresh: bool = True) -> dict[str, pd.DataFrame]:
    """全市場日 K。已有快取者只下載近 1 個月增量。"""
    cfg = settings()["universe"]
    panel: dict[str, pd.DataFrame] = pickle.loads(PANEL_PKL.read_bytes()) if PANEL_PKL.exists() else {}
    if not refresh and panel:
        return panel
    codes = codes if codes is not None else candidate_codes()
    stale = not PANEL_PKL.exists() or time.time() - PANEL_PKL.stat().st_mtime > 3 * 3600
    # 收盤後快取若還沒有今天的 K 棒就必須更新：10/08、10/12、10/13 盤後沿用 13:05 盤中快取（未滿 3 小時），
    # 漲跌歸因整段空白，隔日預掛單也是用前一天的資料算的
    if panel and not stale and time.time() - PANEL_PKL.stat().st_mtime > 20 * 60:  # Yahoo 還沒更新時最多每 20 分鐘重試
        latest = pd.Timestamp(last_closed_session())
        top = [df.index[-1] for df in panel.values() if len(df)]
        stale = sum(d >= latest for d in top) < len(top) * 0.5
    # history_years 調長（例如 5 → 10 年）時，舊快取整批太短：全部重新下載完整歷史
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=365 * cfg["history_years"] - 180)
    if panel and min(df.index[0] for df in panel.values()) > cutoff:
        log.info("快取歷史短於 %d 年，重新下載完整歷史", cfg["history_years"])
        panel = {}
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
