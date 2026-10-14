"""歷史籌碼與月營收資料庫（模型研究 Agent 用）：官方資料逐日／逐月抓取，每個請求一個快取檔，可中斷續傳。

- 三大法人（證交所 T86、櫃買 3itrade_hedge）：每日每檔外資、投信、自營商買賣超（股）
- 月營收（公開資訊觀測站彙總表，上市 sii／上櫃 otc）：每月每家公司營收、年增、月增
  公布時間：法規為次月 10 日前 → 研究時一律當作「次月 11 日起才能用」（不偷看未來）
存放：data/cache/hist/{chip_twse,chip_tpex,revenue}/…csv.gz（data/cache 不進 git）
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import time

import pandas as pd
import requests

from src.config import CACHE_DIR, is_trading_day

log = logging.getLogger(__name__)
ROOT = CACHE_DIR / "hist"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"}
GAP = {"chip_twse": 3.2, "chip_tpex": 2.2, "revenue": 1.5}  # 官方網站限速，太快會被暫時封鎖
_last: dict[str, float] = {}


def _wait(kind: str) -> None:
    g = GAP[kind] - (time.time() - _last.get(kind, 0))
    if g > 0:
        time.sleep(g)
    _last[kind] = time.time()


def _num(x) -> float:
    try:
        return float(str(x).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _path(kind: str, key: str):
    p = ROOT / kind / f"{key}.csv.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def chip_twse(d: dt.date) -> pd.DataFrame:
    p = _path("chip_twse", d.strftime("%Y%m%d"))
    if p.exists():
        return pd.read_csv(p, dtype={"code": str})
    _wait("chip_twse")
    j = requests.get("https://www.twse.com.tw/rwd/zh/fund/T86",
                     params={"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"},
                     headers=UA, timeout=30).json()
    rows = []
    if j.get("stat") == "OK" and j.get("data"):
        f = j["fields"]
        fi = [i for i, x in enumerate(f) if "外" in x and "買賣超" in x]   # 外陸資＋外資自營商（舊格式只有一欄）
        ti = next(i for i, x in enumerate(f) if x.startswith("投信買賣超"))
        di = next((i for i, x in enumerate(f) if x.startswith("自營商買賣超")), None)
        for r in j["data"]:
            rows.append({"code": r[0].strip(), "foreign": sum(_num(r[i]) for i in fi), "trust": _num(r[ti]),
                         "dealer": _num(r[di]) if di is not None else 0.0})
    df = pd.DataFrame(rows, columns=["code", "foreign", "trust", "dealer"])
    df.to_csv(p, index=False, compression="gzip")
    return df


def chip_tpex(d: dt.date) -> pd.DataFrame:
    p = _path("chip_tpex", d.strftime("%Y%m%d"))
    if p.exists():
        return pd.read_csv(p, dtype={"code": str})
    _wait("chip_tpex")
    roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
    j = requests.get("https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php",
                     params={"l": "zh-tw", "se": "EW", "t": "D", "d": roc, "o": "json"}, headers=UA, timeout=30).json()
    rows = []
    for t in j.get("tables") or []:
        data, f = t.get("data") or [], t.get("fields") or []
        if not data:
            continue
        if "外資及陸資淨買股數" in f:  # 2017 年以前的 16 欄格式
            fi, ti, di = f.index("外資及陸資淨買股數"), f.index("投信淨買股數"), f.index("自營淨買股數")
        elif len(f) >= 23:  # 新格式 24 欄：外資合計淨買在第 10 欄、投信第 13 欄、自營合計第 22 欄
            fi, ti, di = 10, 13, 22
        else:
            continue
        for r in data:
            rows.append({"code": str(r[0]).strip(), "foreign": _num(r[fi]), "trust": _num(r[ti]), "dealer": _num(r[di])})
        break
    df = pd.DataFrame(rows, columns=["code", "foreign", "trust", "dealer"])
    df.to_csv(p, index=False, compression="gzip")
    return df


def revenue_month(y: int, m: int, market: str) -> pd.DataFrame:
    p = _path("revenue", f"{market}_{y}{m:02d}")
    if p.exists():
        return pd.read_csv(p, dtype={"code": str})
    _wait("revenue")
    r = requests.get(f"https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{y - 1911}_{m}_0.html", headers=UA, timeout=40)
    r.encoding = "big5"
    rows = []
    if r.status_code == 200:
        try:
            for t in pd.read_html(io.StringIO(r.text)):
                if t.shape[1] < 10:
                    continue
                t.columns = [c[-1] if isinstance(c, tuple) else c for c in t.columns]
                for _, x in t.iterrows():
                    code = str(x.iloc[0]).strip()
                    if not code[:4].isdigit():
                        continue
                    rows.append({"code": code[:6], "revenue": _num(x.iloc[2]), "yoy": _num(x.iloc[6]), "mom": _num(x.iloc[5])})
        except ValueError:
            pass
    df = pd.DataFrame(rows, columns=["code", "revenue", "yoy", "mom"])
    df.to_csv(p, index=False, compression="gzip")
    return df


def backfill(kind: str, start: dt.date, end: dt.date | None = None, budget_sec: float = 6 * 3600) -> int:
    """回補到 end（預設今天）；已有快取的跳過。回傳新抓的請求數。"""
    end = end or dt.date.today()
    t0, n = time.time(), 0
    if kind == "revenue":
        y, m = start.year, start.month
        while (y, m) < (end.year, end.month):
            for mk in ("sii", "otc"):
                if not _path("revenue", f"{mk}_{y}{m:02d}").exists():
                    try:
                        revenue_month(y, m, mk)
                        n += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("月營收 %s %d/%d 失敗：%s", mk, y, m, e)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return n
    fn = chip_twse if kind == "chip_twse" else chip_tpex
    d = start
    while d <= end and time.time() - t0 < budget_sec:
        if is_trading_day(d) and not _path(kind, d.strftime("%Y%m%d")).exists():
            try:
                fn(d)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("%s %s 失敗：%s", kind, d, e)
                time.sleep(20)
        d += dt.timedelta(days=1)
    return n


def load_chip() -> pd.DataFrame:
    """所有已抓的法人資料：date, code, foreign, trust, dealer（股）。"""
    frames = []
    for kind in ("chip_twse", "chip_tpex"):
        for p in sorted((ROOT / kind).glob("*.csv.gz")):
            df = pd.read_csv(p, dtype={"code": str})
            if len(df):
                df["date"] = pd.Timestamp(p.name[:8])
                frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "code", "foreign", "trust", "dealer"])


def load_revenue() -> pd.DataFrame:
    """所有已抓的月營收：code, ym, revenue, yoy, mom, available（次月 11 日）。"""
    frames = []
    for p in sorted((ROOT / "revenue").glob("*.csv.gz")):
        df = pd.read_csv(p, dtype={"code": str})
        if not len(df):
            continue
        ym = p.name.split("_")[1][:6]
        y, m = int(ym[:4]), int(ym[4:])
        df["ym"] = f"{y}-{m:02d}"
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        df["available"] = pd.Timestamp(dt.date(ny, nm, 11))
        frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out.drop_duplicates(["code", "ym"], keep="last") if len(out) else out
