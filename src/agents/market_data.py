"""USMarketAgent / TWMarketAgent：抓取美股隔夜數據與台股行情、法人籌碼。

所有資料都是原始數值，後續分析一律由程式計算，不經 LLM。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import pickle
import time

import pandas as pd
import requests
import yfinance as yf

from src.config import CACHE_DIR, settings, stock_meta, watchlist

log = logging.getLogger(__name__)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def _cached(key: str, fn, max_age_hours: float = 6):
    path = CACHE_DIR / f"{key}.pkl"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        return pickle.loads(path.read_bytes())
    val = fn()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(val))
    return val


# ---------------------------------------------------------------- 美股
def us_tickers() -> list[str]:
    s_all = settings()
    s = s_all["us_watch"]
    tickers = set(s["indices"]) | set(s["stocks"])
    for w in watchlist():
        tickers |= set(w.get("drivers", []))
    for drv in s_all["industry_drivers"].values():
        tickers |= set(drv)
    tickers |= {f["ticker"] for f in s_all["macro_factors"].values()}
    return sorted(tickers)


def us_history(years: int = 2, max_age_hours: float = 2) -> dict[str, pd.DataFrame]:
    """回傳 {ticker: OHLCV DataFrame(index=美國交易日)}。"""

    def fetch():
        out = {}
        for t in us_tickers():
            for attempt in range(3):
                try:
                    df = yf.Ticker(t).history(period=f"{years}y", auto_adjust=False)
                    if not df.empty:
                        df.index = pd.to_datetime(df.index.date)
                        out[t] = df[["Open", "High", "Low", "Close", "Volume"]]
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("yfinance %s 失敗(%s): %s", t, attempt, e)
                    time.sleep(2)
        return out

    tag = hashlib.md5(",".join(us_tickers()).encode()).hexdigest()[:8]
    return _cached(f"us_{years}y_{dt.date.today()}_{tag}", fetch, max_age_hours)


def us_snapshot(hist: dict[str, pd.DataFrame], before: dt.date) -> list[dict]:
    """取「台灣日期 before 之前」最後一個美股交易日的收盤漲跌。"""
    s = settings()["us_watch"]
    names = {**s["indices"], **s["stocks"]}
    rows = []
    for t, name in names.items():
        df = hist.get(t)
        if df is None:
            continue
        df = df[df.index < pd.Timestamp(before)]
        if len(df) < 2:
            continue
        last, prev = df.iloc[-1], df.iloc[-2]
        rows.append({
            "ticker": t, "name": name, "date": df.index[-1].date().isoformat(),
            "close": round(float(last.Close), 2),
            "change_pct": round((last.Close / prev.Close - 1) * 100, 2),
            "chg_5d_pct": round((last.Close / df.Close.iloc[-6] - 1) * 100, 2) if len(df) > 6 else None,
        })
    return rows


def nq_live() -> dict | None:
    """那斯達克期貨即時報價（盤前更新用，僅供參考不進模型）。"""
    try:
        df = yf.Ticker("NQ=F").history(period="2d", interval="5m")
        daily = yf.Ticker("NQ=F").history(period="5d")
        if df.empty or len(daily) < 2:
            return None
        last = float(df.Close.iloc[-1])
        prev_settle = float(daily.Close.iloc[-2])
        return {"price": round(last, 2), "change_pct": round((last / prev_settle - 1) * 100, 2),
                "time": str(df.index[-1])}
    except Exception as e:  # noqa: BLE001
        log.warning("NQ 即時報價失敗: %s", e)
        return None


# ---------------------------------------------------------------- 台股
def _finmind(dataset: str, data_id: str, start: str) -> pd.DataFrame:
    params = {"dataset": dataset, "data_id": data_id, "start_date": start}
    headers = {}
    if os.getenv("FINMIND_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['FINMIND_TOKEN']}"
    for attempt in range(3):
        try:
            r = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
            j = r.json()
            if j.get("status") == 200:
                return pd.DataFrame(j["data"])
            log.warning("FinMind %s %s: %s", dataset, data_id, j.get("msg"))
        except Exception as e:  # noqa: BLE001
            log.warning("FinMind %s %s 失敗: %s", dataset, data_id, e)
        time.sleep(3 * (attempt + 1))
    return pd.DataFrame()


def _yf_tw(code: str, years: int) -> pd.DataFrame:
    suffix = ".TWO" if stock_meta(code).get("market") == "TPEx" else ".TW"
    df = yf.Ticker(code + suffix).history(period=f"{years}y", auto_adjust=False)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index.date)
    return df[["Open", "High", "Low", "Close", "Volume"]]


_PANEL: dict[str, pd.DataFrame] = {}


def set_panel(panel: dict[str, pd.DataFrame]) -> None:
    """注入全市場日 K（UniverseAgent），tw_price 會優先使用，避免重複呼叫 API。"""
    _PANEL.clear()
    _PANEL.update(panel)


def tw_price(code: str, years: int = 2) -> pd.DataFrame:
    """台股日K（index=日期，欄位 Open/High/Low/Close/Volume[股]）。全市場快取 → FinMind → yfinance。"""
    if code in _PANEL and len(_PANEL[code]) > 70:
        return _PANEL[code]

    def fetch():
        start = (dt.date.today() - dt.timedelta(days=365 * years + 10)).isoformat()
        df = _finmind("TaiwanStockPrice", code, start)
        if not df.empty:
            df = df.rename(columns={"open": "Open", "max": "High", "min": "Low", "close": "Close",
                                    "Trading_Volume": "Volume"})
            df.index = pd.to_datetime(df["date"])
            df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            df = df[df.Volume > 0]
            if len(df) > 50:
                return df
        log.info("%s 改用 yfinance", code)
        return _yf_tw(code, years)

    return _cached(f"tw_{code}_{years}y_{dt.date.today()}", fetch, 3)


def tw_institutional(code: str, days: int = 30) -> pd.DataFrame:
    """三大法人每日買賣超（股）。欄位：foreign, trust, dealer, total。"""

    def fetch():
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        df = _finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)
        if df.empty:
            return df
        df["net"] = df["buy"] - df["sell"]
        mapping = {"Foreign_Investor": "foreign", "Foreign_Dealer_Self": "foreign",
                   "Investment_Trust": "trust", "Dealer_self": "dealer", "Dealer_Hedging": "dealer"}
        df["who"] = df["name"].map(mapping).fillna("other")
        p = df.pivot_table(index="date", columns="who", values="net", aggfunc="sum").fillna(0)
        for c in ("foreign", "trust", "dealer"):
            if c not in p:
                p[c] = 0
        p["total"] = p["foreign"] + p["trust"] + p["dealer"]
        p.index = pd.to_datetime(p.index)
        return p[["foreign", "trust", "dealer", "total"]]

    return _cached(f"inst_{code}_{dt.date.today()}", fetch, 3)


def taiex(years: int = 3) -> pd.DataFrame:
    """加權指數日 K。"""

    def fetch():
        df = yf.Ticker("^TWII").history(period=f"{years}y", auto_adjust=False)
        df.index = pd.to_datetime(df.index.date)
        return df[["Open", "High", "Low", "Close", "Volume"]]

    return _cached(f"taiex_{years}y_{dt.date.today()}", fetch, 3)


def tw_revenue(code: str) -> dict:
    """月營收：最新月份年增率、月增率、近 3 個月累計年增率。"""

    def fetch():
        start = (dt.date.today() - dt.timedelta(days=500)).isoformat()
        df = _finmind("TaiwanStockMonthRevenue", code, start)
        if df.empty or len(df) < 13:
            return {}
        df = df.sort_values(["revenue_year", "revenue_month"]).reset_index(drop=True)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        ly = df[(df.revenue_year == last.revenue_year - 1) & (df.revenue_month == last.revenue_month)]
        last3 = df.tail(3).revenue.sum()
        ly3 = df.iloc[-15:-12].revenue.sum() if len(df) >= 15 else None
        if ly.empty:
            return {}
        return {"month": f"{int(last.revenue_year)}/{int(last.revenue_month):02d}",
                "revenue_100m": round(float(last.revenue) / 1e8, 2),
                "yoy": round(float(last.revenue / ly.revenue.iloc[0] - 1), 4),
                "mom": round(float(last.revenue / prev.revenue - 1), 4),
                "yoy_3m": round(float(last3 / ly3 - 1), 4) if ly3 else None}

    return _cached(f"rev_{code}_{dt.date.today()}", fetch, 12)


def tw_valuation(code: str) -> dict:
    """本益比、股價淨值比、殖利率（最新一日；ETF 無資料）。"""

    def fetch():
        df = _finmind("TaiwanStockPER", code, (dt.date.today() - dt.timedelta(days=20)).isoformat())
        if df.empty:
            return {}
        r = df.iloc[-1]
        return {"date": str(r["date"]), "per": float(r["PER"]), "pbr": float(r["PBR"]),
                "dividend_yield": float(r["dividend_yield"])}

    return _cached(f"val_{code}_{dt.date.today()}", fetch, 12)


def tw_margin(code: str) -> dict:
    """融資融券餘額（張）與近 5 日變化。"""

    def fetch():
        df = _finmind("TaiwanStockMarginPurchaseShortSale", code, (dt.date.today() - dt.timedelta(days=20)).isoformat())
        if df.empty:
            return {}
        last = df.iloc[-1]
        base = df.iloc[-6] if len(df) >= 6 else df.iloc[0]
        return {"date": str(last["date"]), "margin_balance": int(last["MarginPurchaseTodayBalance"]),
                "margin_chg5": int(last["MarginPurchaseTodayBalance"] - base["MarginPurchaseTodayBalance"]),
                "short_balance": int(last["ShortSaleTodayBalance"]),
                "short_chg5": int(last["ShortSaleTodayBalance"] - base["ShortSaleTodayBalance"])}

    return _cached(f"margin_{code}_{dt.date.today()}", fetch, 12)


def atr(df: pd.DataFrame, n: int = 14) -> float:
    prev_close = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - prev_close).abs(), (df.Low - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def tw_features(code: str, asof: dt.date | None = None) -> dict:
    """單一個股的技術面與籌碼面數值（全部程式計算）。asof 之前（不含）的資料。"""
    df = tw_price(code)
    if asof is not None:
        df = df[df.index < pd.Timestamp(asof)]
    if len(df) < 70:
        return {}
    c = df.Close
    inst = tw_institutional(code)
    if asof is not None and not inst.empty:
        inst = inst[inst.index < pd.Timestamp(asof)]
    avg_vol20 = float(df.Volume.tail(20).mean())
    inst5 = float(inst["total"].tail(5).sum()) if not inst.empty else 0.0
    foreign5 = float(inst["foreign"].tail(5).sum()) if not inst.empty else 0.0
    trust5 = float(inst["trust"].tail(5).sum()) if not inst.empty else 0.0
    buy_streak = 0
    if not inst.empty:
        for v in reversed(inst["total"].tolist()):
            if v > 0:
                buy_streak += 1
            else:
                break
    return {
        "date": df.index[-1].date().isoformat(),
        "close": float(c.iloc[-1]),
        "prev_close": float(c.iloc[-2]),
        "ret_1d": float(c.iloc[-1] / c.iloc[-2] - 1),
        "ret_5d": float(c.iloc[-1] / c.iloc[-6] - 1),
        "ret_20d": float(c.iloc[-1] / c.iloc[-21] - 1),
        "ma20": float(c.tail(20).mean()),
        "ma60": float(c.tail(60).mean()),
        "atr14": atr(df),
        "low10": float(df.Low.tail(10).min()),
        "high20": float(df.High.tail(20).max()),
        "avg_vol20": avg_vol20,
        "vol_ratio": float(df.Volume.iloc[-1] / avg_vol20) if avg_vol20 else 1.0,
        "inst_net5": inst5,
        "foreign_net5": foreign5,
        "trust_net5": trust5,
        "inst_net5_ratio": inst5 / (avg_vol20 * 5) if avg_vol20 else 0.0,
        "inst_buy_streak": buy_streak,
    }
